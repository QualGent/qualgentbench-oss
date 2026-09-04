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

from . import pricing, submission
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

_RESULT_RE = re.compile(r"RESULT:\s*verdict\s*=\s*(?P<v>pass|fail)", re.I)


# ── loading ────────────────────────────────────────────────────────────────────

def cases_path(app_id: str) -> Path:
    return _CASES_DIR / f"{app_id}.yaml"


def truth_path(app_id: str) -> Path:
    return _TRUTH_DIR / f"journey-{app_id}.json"


def load_cases(app_id: str) -> dict | None:
    p = cases_path(app_id)
    return yaml.safe_load(p.read_text()) if p.exists() else None


def load_truth(app_id: str) -> dict:
    p = truth_path(app_id)
    return json.loads(p.read_text()) if p.exists() else {}


def has_cases(app_id: str) -> bool:
    return cases_path(app_id).exists()


def apk_meta(app_id: str) -> dict | None:
    """The journey build of an app: the test-case file's `apk:` block (published
    under journey/ on HuggingFace, sha256-verified). Journey-only defects live in
    this build, not in the hunt build the benchmark spec points at."""
    doc = load_cases(app_id)
    meta = (doc or {}).get("apk")
    return dict(meta) if isinstance(meta, dict) and meta.get("filename") else None


def task_id(case_id: str, version: str) -> str:
    return f"{case_id}~{version}"


def split_task_id(tid: str) -> tuple[str, str]:
    """`case~version` → (case, version). A bare id is the seeded version (old runs)."""
    if "~" in tid:
        case, version = tid.rsplit("~", 1)
        if version in VERSIONS:
            return case, version
    return tid, "seeded"


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
            "expected": "FAIL" if blocking else "PASS"}


def _oracle(case: dict) -> dict:
    """The completion oracle: the case's `check.expect`, plus `evidence` strings for
    outcomes that can only be read off a screen (the agent's own device output must
    contain them). An `absent:` outcome needs explicit evidence."""
    expect = dict(((case.get("check") or {}).get("expect")) or {})
    evidence = [str(e) for e in (case.get("evidence") or [])]
    if "db" in expect:
        mode = "db"
    elif "content" in expect:
        mode = "content"                 # ContentProvider query, evaluated on the device like db
    elif "present" in expect:
        mode = "present"
        evidence = evidence or [str(expect["present"])]
    elif "absent" in expect:
        mode = "absent"
    else:
        mode = "none"
    return {"mode": mode, "expect": expect, "evidence": evidence}


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
        blocking_texts = sorted({t for d in measured.get("unclaimed_diff", [])
                                 for t in d.get("added", []) + d.get("removed", [])}) if design["blocking"] else []
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
                "blocking_texts": blocking_texts if seeded else [],
                "side": side if seeded else [],
                "defects": defects,
                "oracle": _oracle(case),
                "truth_agrees": measured.get("agrees") if measured else None,
                "device_setup": suite.get("device_setup"),
                "shared_storage": suite.get("shared_storage"),
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
    if tooling == "raw":
        tooling_note = ("Use the tools available in your environment to operate the device "
                        "(for example the `adb` command line).")
    else:
        tooling_note = (
            "MCP tools are available for device control. Every tool takes the "
            f'device as its first argument — always pass device="{device_serial}".')
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
    grounded: bool = False          # `observed` appeared in the agent's own device output

    @property
    def text(self) -> str:
        return " ".join([self.screen, self.observed, self.expected, self.description]).lower()

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


def _word(sym: str, text: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(sym) + r"(?![a-z0-9])", text) is not None


def match_report(bug: BugReport, spec: dict) -> str | None:
    """Which bug ON THIS BUILD does this report describe? Side markers and measured
    texts first (specific), then the blocking bug's texts, then the symptom
    vocabulary on whole words — 'age' must not match inside 'average'. On a clean
    build nothing is active, so every report is a false report."""
    observed = _norm(bug.observed)
    text = bug.text
    active = set(spec.get("active_bugs") or [])
    if not active:
        return None
    for s in spec.get("side") or []:
        if s["bug"] not in active:
            continue
        marker = _norm(s.get("marker") or "")
        if (marker and marker in observed) or any(_norm(t) in observed for t in s.get("texts") or []):
            return s["bug"]
    blocking = spec.get("blocking")
    if blocking and blocking in active:
        if any(_norm(t) and _norm(t) in observed for t in spec.get("blocking_texts") or []):
            return blocking
    defects = spec.get("defects") or {}
    ordered = ([blocking] if blocking else []) + [s["bug"] for s in spec.get("side") or []]
    ordered += [d for d in defects if d not in ordered]
    for bug_id in ordered:
        if bug_id not in active:
            continue
        for sym in (defects.get(bug_id) or {}).get("symptoms") or []:
            if sym and _word(sym, text):
                return bug_id
    return None


# ── the scorer ─────────────────────────────────────────────────────────────────

def _device_texts(transcript: str, tooling: str) -> list[str]:
    from .bugs import _ordered_stream
    return [p for kind, p in _ordered_stream(transcript, tooling) if kind == "device"]


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
    if mode in ("db", "content"):
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

    for b in report.bugs:
        obs = _norm(b.observed)
        b.grounded = len(obs) >= 3 and any(obs in t for t in device_texts)
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
    reasons: list[str] = []
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
    else:
        completed = reported == "pass" and oracle_ok is not False
        if reported != "pass":
            reasons.append("the expected outcome holds on this build, but the agent reported fail")
        if oracle_ok is False:
            reasons.append(f"the outcome was not reached — {oracle_why}")
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
    passed = bool(completed and not missed and false_reports == 0)

    tool_calls = _count_tool_calls(transcript)
    steps = spec.get("hook_steps") or (device_actions if tooling else tool_calls)
    budget = spec.get("step_budget")

    usage = parser.token_usage()
    reported_cost = usage.get("reported_cost_usd")
    cost = reported_cost if reported_cost is not None else pricing.compute_cost_usd(model, usage)

    metrics = {
        "version": version,
        "case_id": spec.get("case_id"),
        "app_id": spec.get("app_id"),
        # instruction following
        "completed": completed,
        "completion_reason": "; ".join(reasons) if not completed else "",
        "oracle": {"mode": (spec.get("oracle") or {}).get("mode"), "ok": oracle_ok, "why": oracle_why},
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
        **contamination.as_metrics(),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": cost,
        "cost_source": "reported" if reported_cost is not None
                       else ("estimated" if cost is not None else "unknown"),
    }
    return VerifierResult(
        passed=passed,
        score=1.0 if completed else 0.0,
        weighted_score=(recall if recall is not None else (1.0 if false_reports == 0 else 0.0)),
        criteria={"completed": completed,
                  "all_bugs_found": not missed,
                  "no_false_reports": false_reports == 0,
                  "evidence": evidence},
        failure_reason="; ".join(reasons) or None,
        metrics=metrics,
    )


# ── the board ──────────────────────────────────────────────────────────────────

def _row(key: tuple, rs: list) -> dict[str, Any]:
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
    return {
        "episodes": len(m),
        "clean_episodes": len(clean),
        "clean_completed": sum(1 for x in clean if x.get("completed")),
        "seeded_episodes": len(seeded),
        "seeded_completed": sum(1 for x in seeded if x.get("completed")),
        "completion": round(sum(1 for x in m if x.get("completed")) / len(m), 4) if m else None,
        "bugs_present": present,
        "bugs_found": found,
        "false_reports": fp,
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
        "avg_steps": round(sum(steps) / len(steps), 1) if steps else None,
        "avg_tokens": round(sum(x.get("total_tokens") or 0 for x in m) / len(m)) if m else None,
    }


def summary(results, by_app: bool = False) -> list[dict[str, Any]]:
    """The journey board as data: one row per (agent, model, condition), or per
    (agent, model, condition, app) with `by_app`. Excluded episodes are dropped."""
    from .failures import is_excluded
    from .leaderboard import clean_model_name

    groups: dict[tuple, list] = {}
    for r in results:
        if r.task_type != TASK_TYPE or is_excluded(r.metrics or {}):
            continue
        key = (r.agent, clean_model_name(r.model), r.condition)
        if by_app:
            key = key + ((r.metrics or {}).get("app_id") or split_task_id(r.task_id)[0].split("-")[0],)
        groups.setdefault(key, []).append(r)
    rows = []
    for key, rs in groups.items():
        row = {"agent": key[0], "model": key[1], "condition": key[2]}
        if by_app:
            row["app"] = key[3]
        row.update(_row(key, rs))
        rows.append(row)
    return sorted(rows, key=lambda r: (-(r["completion"] or 0), -(r["f1"] or 0)))
